"""France Travail API source tests using mocked HTTP.

FakeSession here differs from test_board_scraper.py's: it needs to see
`data=` (the token POST is form-encoded) and `params=` (the search GET's
query string), which board scraping never sends.
"""

from __future__ import annotations

import json

import pytest

from job_scraper import paths
from job_scraper.api_sources import france_travail
from job_scraper.api_sources.france_travail import TOKEN_URL, SEARCH_URL
from job_scraper.post_scraper import fetch_job


class FakeResponse:
    def __init__(self, body):
        self.status_code = 200 if body is not None else 404
        self._body = (body or "").encode()
        self.encoding = "utf-8"
        self.headers = {"Content-Type": "application/json"}
        self.raw = self

    def read(self, amount, decode_content=True):
        return self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, token_body, search_pages):
        """search_pages: list of JSON bodies, one per expected search call."""
        self.token_body = token_body
        self.search_pages = list(search_pages)
        self.requests = []

    def post(self, url, data=None, headers=None, **kwargs):
        self.requests.append(("post", url, data))
        return FakeResponse(self.token_body)

    def get(self, url, params=None, headers=None, **kwargs):
        self.requests.append(("get", url, params, headers or {}))
        body = self.search_pages.pop(0) if self.search_pages else None
        return FakeResponse(body)


@pytest.fixture(autouse=True)
def _reset_token_cache():
    france_travail._token = None
    yield
    france_travail._token = None


@pytest.fixture
def credentials_file(tmp_path, monkeypatch):
    path = tmp_path / "france_travail_credentials.json"
    path.write_text(json.dumps({
        "client_id": "id", "client_secret": "secret",
    }))
    monkeypatch.setattr(paths, "FRANCE_TRAVAIL_CREDENTIALS", path)
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


def test_no_credentials_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        paths, "FRANCE_TRAVAIL_CREDENTIALS", tmp_path / "missing.json"
    )
    session = FakeSession(token_body=None, search_pages=[])

    assert france_travail.fetch_jobs(session) == []
    assert session.requests == []


def test_fetches_and_maps_jobs(credentials_file, monkeypatch):
    monkeypatch.setattr(france_travail, "get_keywords", lambda: ["python"])
    session = FakeSession(
        token_body=json.dumps({"access_token": "tok", "expires_in": 3600}),
        search_pages=[json.dumps({"resultats": [_offer()]})],
    )

    jobs = france_travail.fetch_jobs(session)

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
    assert search_call[3]["Authorization"] == "Bearer tok"


def test_paginates_until_short_page(credentials_file, monkeypatch):
    monkeypatch.setattr(france_travail, "get_keywords", lambda: [])
    monkeypatch.setattr(france_travail, "PAGE_SIZE", 1)
    session = FakeSession(
        token_body=json.dumps({"access_token": "tok", "expires_in": 3600}),
        search_pages=[
            json.dumps({"resultats": [_offer(url="https://x/1")]}),
            json.dumps({"resultats": []}),
        ],
    )

    jobs = france_travail.fetch_jobs(session)

    assert [j.url for j in jobs] == ["https://x/1"]
    assert sum(1 for r in session.requests if r[0] == "get") == 2


def test_token_is_cached_across_calls(credentials_file, monkeypatch):
    monkeypatch.setattr(france_travail, "get_keywords", lambda: [])
    session = FakeSession(
        token_body=json.dumps({"access_token": "tok", "expires_in": 3600}),
        search_pages=[
            json.dumps({"resultats": []}),
            json.dumps({"resultats": []}),
        ],
    )

    france_travail.fetch_jobs(session)
    france_travail.fetch_jobs(session)

    assert sum(1 for r in session.requests if r[0] == "post") == 1


def test_bad_token_response_returns_empty(credentials_file):
    session = FakeSession(token_body="not json", search_pages=[])

    assert france_travail.fetch_jobs(session) == []


def test_post_scraper_shortcircuits_api_source_rows():
    row = {
        "url": "https://example.fr/offre/1",
        "title": "Dev",
        "description": "full posting text",
        "ats": "france_travail",
    }

    job = fetch_job(row)

    assert job.description == "full posting text"
    assert job.via == "france_travail"
